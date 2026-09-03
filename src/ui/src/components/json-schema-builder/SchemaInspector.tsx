import React, { useState, useEffect } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  FormField,
  Header,
  Input,
  Multiselect,
  Select,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Textarea,
} from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import StringConstraints from './constraints/StringConstraints';
import NumberConstraints from './constraints/NumberConstraints';
import ArrayConstraints from './constraints/ArrayConstraints';
import ObjectConstraints from './constraints/ObjectConstraints';
import MetadataFields from './constraints/MetadataFields';
import ValueConstraints from './constraints/ValueConstraints';
import ExamplesEditor from './constraints/ExamplesEditor';
import PageTypesEditor, { PageTypeEntry } from './constraints/PageTypesEditor';
import {
  TYPE_OPTIONS,
  EVALUATION_METHOD_OPTIONS,
  EVALUATION_THRESHOLD_DEFAULTS,
  EVALUATION_MATCH_THRESHOLD_DEFAULTS,
  METHODS_REQUIRING_THRESHOLD,
  METHODS_REQUIRING_MATCH_THRESHOLD,
  EVALUATION_METHOD_HUNGARIAN,
  X_AWS_IDP_DOCUMENT_TYPE,
  X_AWS_IDP_EVALUATION_METHOD,
  X_AWS_IDP_EVALUATION_THRESHOLD,
  X_AWS_IDP_EVALUATION_WEIGHT,
  X_AWS_IDP_EVALUATION_MATCH_THRESHOLD,
  X_AWS_IDP_CONFIDENCE_THRESHOLD,
  X_AWS_IDP_EXAMPLES,
  X_AWS_IDP_DOCUMENT_NAME_REGEX,
  X_AWS_IDP_PAGE_CONTENT_REGEX,
  X_AWS_IDP_EXTRACTION_MODEL,
  X_AWS_IDP_EXTRACTION_ESCALATION_MODEL,
  X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT,
  X_AWS_IDP_EXTRACTION_TASK_PROMPT,
  EXTRACTION_MODEL_OVERRIDE_OPTIONS,
  X_AWS_IDP_EXCLUDE_FROM_PROCESSING,
  X_AWS_IDP_EXCLUSION_REASON,
  X_AWS_IDP_INSTANCE_ARRAY,
  X_AWS_IDP_PAGE_TYPES,
  X_AWS_IDP_SOURCE_PAGE_TYPES,
  X_AWS_IDP_VALIDATION_ENGINE,
  X_AWS_IDP_RULE_ID,
  X_AWS_IDP_RULE_JSON,
  VALIDATION_ENGINE_OPTIONS,
} from '../../constants/schemaConstants';

interface SchemaAttribute {
  type?: string;
  $ref?: string;
  description?: string;
  properties?: Record<string, unknown>;
  required?: string[];
  minProperties?: number;
  maxProperties?: number;
  additionalProperties?: boolean | Record<string, unknown>;
  items?: {
    type?: string;
    $ref?: string;
    properties?: Record<string, unknown>;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

/**
 * A few-shot example entry. Field keys are the canonical `x-aws-idp-*` names
 * (legacy camelCase keys are still read) — see ExamplesEditor.
 */
type Example = Record<string, unknown>;

interface SchemaClass {
  id: string;
  name: string;
  description?: string;
  attributes?: {
    properties?: Record<string, SchemaAttribute>;
    required?: string[];
  };
  [key: string]: unknown;
}

interface UsageInfo {
  className: string;
  classId: string;
  attributeName: string;
  type: string;
}

interface SchemaInspectorProps {
  selectedClass?: SchemaClass | null;
  selectedAttribute?: SchemaAttribute | null;
  selectedAttributeName?: string | null;
  onUpdate: (updates: Partial<SchemaAttribute>) => void;
  onUpdateClass?: (updates: Record<string, unknown>) => void;
  onRenameAttribute?: (newName: string) => boolean;
  availableClasses?: SchemaClass[];
  isRequired?: boolean;
  onToggleRequired?: (checked: boolean) => void;
  onNavigateToClass?: ((classId: string) => void) | null;
  onNavigateToAttribute?: ((classId: string, attributeName: string | null) => void) | null;
  isRuleSchema?: boolean;
}

// GraphQL mutation for generating RuleJSON
const GENERATE_RULE_JSON_MUTATION = /* GraphQL */ `
  mutation GenerateRuleJson($ruleDescription: String!) {
    generateRuleJson(ruleDescription: $ruleDescription) {
      success
      ruleJson
      error {
        type
        message
      }
    }
  }
`;

/**
 * Sub-component for Z3 RuleJSON generation and display.
 * Shows Generate button + JSON viewer/editor when engine is Z3.
 */
const RuleJsonSection: React.FC<{
  selectedAttribute: SchemaAttribute;
  onUpdate: (updates: Partial<SchemaAttribute>) => void;
}> = ({ selectedAttribute, onUpdate }) => {
  const [isGenerating, setIsGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState('');

  const existingRuleJson = selectedAttribute[X_AWS_IDP_RULE_JSON] as Record<string, unknown> | undefined;
  const ruleDescription = (selectedAttribute.description as string) || '';

  const handleGenerate = async () => {
    if (!ruleDescription.trim()) {
      setGenerateError('Rule description is required. Add a description first.');
      return;
    }
    setIsGenerating(true);
    setGenerateError(null);
    try {
      const client = generateClient();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const result: any = await client.graphql({
        query: GENERATE_RULE_JSON_MUTATION,
        variables: { ruleDescription: ruleDescription.trim() },
      });
      const response = result.data?.generateRuleJson;
      if (response?.success && response.ruleJson) {
        const parsed = JSON.parse(response.ruleJson);
        onUpdate({ [X_AWS_IDP_RULE_JSON]: parsed });
        setGenerateError(null);
      } else {
        setGenerateError(response?.error?.message || 'Failed to generate RuleJSON');
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Unexpected error';
      setGenerateError(message);
    } finally {
      setIsGenerating(false);
    }
  };

  const handleSaveEdit = () => {
    try {
      const parsed = JSON.parse(editValue);
      onUpdate({ [X_AWS_IDP_RULE_JSON]: parsed });
      setIsEditing(false);
      setGenerateError(null);
    } catch {
      setGenerateError('Invalid JSON. Please fix syntax errors before saving.');
    }
  };

  return (
    <SpaceBetween size="s">
      <FormField
        label="RuleJSON (Symbolic Z3)"
        description="SMT-LIB constraints generated from the rule description. Required for Z3 validation."
      >
        {existingRuleJson ? (
          <SpaceBetween size="xs">
            <StatusIndicator type="success">RuleJSON configured</StatusIndicator>
            {!isEditing ? (
              <SpaceBetween size="xs" direction="horizontal">
                <Button onClick={handleGenerate} loading={isGenerating} iconName="refresh">
                  Regenerate
                </Button>
                <Button
                  onClick={() => {
                    setEditValue(JSON.stringify(existingRuleJson, null, 2));
                    setIsEditing(true);
                  }}
                  iconName="edit"
                >
                  Edit
                </Button>
                <Button onClick={() => onUpdate({ [X_AWS_IDP_RULE_JSON]: undefined })} variant="link">
                  Remove
                </Button>
              </SpaceBetween>
            ) : (
              <SpaceBetween size="xs">
                <Textarea value={editValue} onChange={({ detail }) => setEditValue(detail.value)} rows={12} />
                <SpaceBetween size="xs" direction="horizontal">
                  <Button onClick={handleSaveEdit} variant="primary">
                    Save
                  </Button>
                  <Button onClick={() => setIsEditing(false)}>Cancel</Button>
                </SpaceBetween>
              </SpaceBetween>
            )}
            {!isEditing && <Textarea value={JSON.stringify(existingRuleJson, null, 2)} readOnly rows={8} />}
          </SpaceBetween>
        ) : (
          <SpaceBetween size="xs">
            <div title="Translates the natural language rule description into formal SMT-LIB constraints that the Z3 solver uses for deterministic validation">
              <Button onClick={handleGenerate} loading={isGenerating} variant="primary" iconName="gen-ai">
                Generate RuleJSON
              </Button>
            </div>
            {isGenerating && (
              <Box>
                <Spinner /> Translating rule to SMT-LIB constraints...
              </Box>
            )}
          </SpaceBetween>
        )}
      </FormField>
      {generateError && <Alert type="error">{generateError}</Alert>}
    </SpaceBetween>
  );
};

/**
 * Names of the class's own top-level properties that are an ARRAY OF OBJECTS.
 *
 * These are the only legal values for `x-aws-idp-instance-array` (Designate
 * mode): the backend config validator
 * (`IDPConfig.validate_instance_array` in `config/models.py`) hard-rejects a
 * name that is not a top-level array-of-object, so offering a free-text box
 * here would let a user save a config the pipeline then refuses to load.
 * A `$ref`'d `items` counts — that is the idiom this editor itself emits for a
 * reusable record type, and the validator resolves it.
 */
const arrayOfObjectPropertyNames = (cls: SchemaClass): string[] => {
  const properties = cls.attributes?.properties || {};
  return Object.entries(properties)
    .filter(([, spec]) => {
      if (!spec || spec.type !== 'array') return false;
      const items = spec.items;
      if (!items) return false;
      return Boolean(items.$ref) || items.type === 'object' || Boolean(items.properties);
    })
    .map(([name]) => name);
};

const NONE_OPTION_VALUE = '';

/**
 * Class-level editor for `x-aws-idp-instance-array` ("Designate mode").
 *
 * Before this control the key was round-tripped by the schema editor but had no
 * UI at all, so the only way to reach it was pasting JSON or editing the YAML —
 * see GitHub #694 / #715. It is a SELECT rather than a text input because the
 * value must name one of the class's own array-of-object properties; a typo in a
 * free-text box would be accepted here and then hard-fail config load.
 */
const InstanceArrayField = ({
  selectedClass,
  onUpdateClass,
}: {
  selectedClass: SchemaClass;
  onUpdateClass: (updates: Record<string, unknown>) => void;
}): React.JSX.Element => {
  const candidates = arrayOfObjectPropertyNames(selectedClass);
  const current = (selectedClass[X_AWS_IDP_INSTANCE_ARRAY] as string) || '';
  // A value set outside the UI (YAML/CLI) that no longer matches an
  // array-of-object property is kept and flagged rather than dropped — silently
  // erasing a user's configuration on open is the failure mode this whole key
  // has already had once.
  const isStale = Boolean(current) && !candidates.includes(current);

  const options = [
    { label: '(None — one document per section)', value: NONE_OPTION_VALUE },
    ...candidates.map((name) => ({ label: name, value: name })),
    ...(isStale ? [{ label: `${current} (not an array of objects)`, value: current }] : []),
  ];

  return (
    <FormField
      label="Instance Array (Optional)"
      description={
        candidates.length === 0 && !isStale
          ? 'Only for a class whose schema is already a PACKET of records — it names the top-level array-of-objects property holding one record per document. This class has no array-of-objects property, so there is nothing to designate.'
          : 'For a class whose schema is already a PACKET of records: names the top-level array-of-objects property that holds one record per document. The section then reports that array’s length as its instance count (shown as a badge in the Sections panel) instead of always 1. Purely a signal — it does not change the extraction shape or any downstream output.'
      }
      errorText={
        isStale
          ? `"${current}" is not a top-level array-of-objects property of this class; the configuration will be rejected on save.`
          : undefined
      }
    >
      <Select
        selectedOption={options.find((o) => o.value === current) || options[0]}
        onChange={({ detail }) => onUpdateClass({ [X_AWS_IDP_INSTANCE_ARRAY]: detail.selectedOption.value || undefined })}
        options={options}
        disabled={candidates.length === 0 && !isStale}
        placeholder="(None — one document per section)"
      />
    </FormField>
  );
};

const SchemaInspector = ({
  selectedClass = null,
  selectedAttribute = null,
  selectedAttributeName = null,
  onUpdate,
  onUpdateClass = () => {},
  onRenameAttribute = () => true,
  availableClasses = [],
  isRequired = false,
  onToggleRequired = () => {},
  onNavigateToClass = null,
  onNavigateToAttribute = null,
  isRuleSchema = false,
}: SchemaInspectorProps): React.JSX.Element => {
  // Dynamic labels based on schema type
  const typeLabel = isRuleSchema ? 'policy' : 'document';
  const TypeLabel = isRuleSchema ? 'Policy' : 'Document';

  // Show class-level settings when class is selected but no attribute is selected
  if (selectedClass && (!selectedAttribute || !selectedAttributeName)) {
    // Find where this class is being used
    const usedIn: UsageInfo[] = [];
    if (availableClasses) {
      availableClasses.forEach((cls) => {
        if (cls.id === selectedClass.id) return; // Skip self

        const properties = cls.attributes?.properties || {};
        Object.entries(properties).forEach(([attrName, attrSchema]) => {
          // Check if attribute references this class
          if (attrSchema.$ref === `#/$defs/${selectedClass.name}`) {
            usedIn.push({
              className: cls.name,
              classId: cls.id,
              attributeName: attrName,
              type: 'object',
            });
          } else if (attrSchema.items?.$ref === `#/$defs/${selectedClass.name}`) {
            usedIn.push({
              className: cls.name,
              classId: cls.id,
              attributeName: attrName,
              type: 'array',
            });
          }
        });
      });
    }

    return (
      <Box>
        <Header variant="h3">{isRuleSchema ? 'Policy Class Properties' : `Class Inspector: ${selectedClass.name}`}</Header>
        <SpaceBetween size="m">
          <FormField
            label={`${TypeLabel} Type`}
            description={`${TypeLabel} types become top-level schemas. ${
              isRuleSchema ? 'Uncheck this for non-policy-type classes.' : 'Shared classes are reusable definitions.'
            }`}
          >
            <Checkbox
              checked={(selectedClass[X_AWS_IDP_DOCUMENT_TYPE] as boolean) || false}
              onChange={({ detail }) => onUpdateClass({ [X_AWS_IDP_DOCUMENT_TYPE]: detail.checked })}
            >
              This is a {typeLabel} type
            </Checkbox>
          </FormField>

          {selectedClass[X_AWS_IDP_DOCUMENT_TYPE] ? (
            <Alert type="info">
              <strong>{TypeLabel} Type</strong>
              <br />
              This class will be exported as a standalone JSON schema. Each {typeLabel} type schema will only include $defs for classes it
              actually references, keeping schemas minimal and focused.
            </Alert>
          ) : (
            !isRuleSchema && (
              <Alert type="info">
                <strong>Shared Class</strong>
                <br />
                This class is available to be referenced by {typeLabel} types and other classes. It will only appear in the $defs section of
                schemas that reference it.
              </Alert>
            )
          )}

          <FormField
            label={isRuleSchema ? 'Policy Class Description' : 'Class Description'}
            description="Describe the purpose of this class"
          >
            <Textarea
              value={selectedClass.description || ''}
              onChange={({ detail }) => onUpdateClass({ description: detail.value || undefined })}
              rows={3}
              placeholder="Describe what this class represents"
            />
          </FormField>

          {!!selectedClass[X_AWS_IDP_DOCUMENT_TYPE] && (
            <>
              {!isRuleSchema && (
                <ExamplesEditor
                  examples={(selectedClass[X_AWS_IDP_EXAMPLES] as Example[]) || []}
                  onChange={(examples) => onUpdateClass({ [X_AWS_IDP_EXAMPLES]: examples })}
                />
              )}

              {!isRuleSchema && (
                <PageTypesEditor
                  pageTypes={(selectedClass[X_AWS_IDP_PAGE_TYPES] as PageTypeEntry[]) || []}
                  onChange={(pageTypes) => onUpdateClass({ [X_AWS_IDP_PAGE_TYPES]: pageTypes.length > 0 ? pageTypes : undefined })}
                />
              )}

              <FormField
                label="Document Name Regex"
                description={
                  isRuleSchema
                    ? 'Pattern to match document ID/name. When matched, instantly classifies all pages as this type policy (single-class configs only). Use case-insensitive patterns like (?i).*(medicare|global).*'
                    : `Pattern to match ${typeLabel} ID/name. When matched, instantly classifies all pages as this type (single-class configs only). Use case-insensitive patterns like (?i).*(invoice|bill).*`
                }
              >
                <Input
                  value={(selectedClass[X_AWS_IDP_DOCUMENT_NAME_REGEX] as string) || ''}
                  onChange={({ detail }) => onUpdateClass({ [X_AWS_IDP_DOCUMENT_NAME_REGEX]: detail.value || undefined })}
                  placeholder={isRuleSchema ? 'e.g., (?i).*(medicare|global).*' : 'e.g., (?i).*(invoice|bill).*'}
                />
              </FormField>

              <FormField
                label="Page Content Regex"
                description={
                  isRuleSchema
                    ? 'Pattern to match page text content. When matched, classifies the document as this policy. Use case-insensitive patterns like (?i)(medicare\\s+number|amount\\s+due)'
                    : 'Pattern to match page text content. When matched during page-level classification, classifies the page as this type. Use case-insensitive patterns like (?i)(invoice\\s+number|amount\\s+due)'
                }
              >
                <Input
                  value={(selectedClass[X_AWS_IDP_PAGE_CONTENT_REGEX] as string) || ''}
                  onChange={({ detail }) => onUpdateClass({ [X_AWS_IDP_PAGE_CONTENT_REGEX]: detail.value || undefined })}
                  placeholder={isRuleSchema ? 'e.g., (?i)(medicare\\s+number)' : 'e.g., (?i)(invoice\\s+number|bill\\s+to)'}
                />
              </FormField>

              {!isRuleSchema && <InstanceArrayField selectedClass={selectedClass} onUpdateClass={onUpdateClass} />}

              {!isRuleSchema && (
                <>
                  <FormField
                    label="Exclude from Processing (Optional)"
                    description="When checked, sections classified as this class are SKIPPED by downstream stages (extraction, assessment, summarization, rule validation, evaluation). Use for static boilerplate pages (instructions, legal notices, cover pages) that carry no extractable data."
                  >
                    <Checkbox
                      checked={Boolean(selectedClass[X_AWS_IDP_EXCLUDE_FROM_PROCESSING])}
                      onChange={({ detail }) =>
                        onUpdateClass({
                          [X_AWS_IDP_EXCLUDE_FROM_PROCESSING]: detail.checked || undefined,
                        })
                      }
                    >
                      Skip downstream processing for this class
                    </Checkbox>
                  </FormField>

                  <FormField
                    label="Exclusion Reason (Optional)"
                    description='Short category shown in the UI Sections panel as a "Skipped: <reason>" badge and in the evaluation markdown report. Common values: "instructions", "legal", "cover-page", "boilerplate". Only used when "Exclude from Processing" is checked.'
                  >
                    <Input
                      value={(selectedClass[X_AWS_IDP_EXCLUSION_REASON] as string) || ''}
                      onChange={({ detail }) => onUpdateClass({ [X_AWS_IDP_EXCLUSION_REASON]: detail.value || undefined })}
                      placeholder="e.g., instructions"
                      disabled={!selectedClass[X_AWS_IDP_EXCLUDE_FROM_PROCESSING]}
                    />
                  </FormField>

                  <FormField
                    label="Extraction Model Override (Optional)"
                    description="Override the global extraction model for this class. When set, this model is used instead of the global extraction.model setting. Select empty to use the default."
                  >
                    <Select
                      selectedOption={
                        (selectedClass[X_AWS_IDP_EXTRACTION_MODEL] as string)
                          ? {
                              label: selectedClass[X_AWS_IDP_EXTRACTION_MODEL] as string,
                              value: selectedClass[X_AWS_IDP_EXTRACTION_MODEL] as string,
                            }
                          : { label: '(Use global default)', value: '' }
                      }
                      onChange={({ detail }) => onUpdateClass({ [X_AWS_IDP_EXTRACTION_MODEL]: detail.selectedOption.value || undefined })}
                      options={EXTRACTION_MODEL_OVERRIDE_OPTIONS}
                      filteringType="auto"
                      placeholder="Select model override"
                    />
                  </FormField>

                  <FormField
                    label="Escalation Model Override (Optional)"
                    description="When Schema Validation is enabled with Fail Action 'escalate', failing fields for this class are re-extracted with this stronger model. Overrides the global extraction.agentic.validation.escalation_model. Select empty to use the global default."
                  >
                    <Select
                      selectedOption={
                        (selectedClass[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL] as string)
                          ? {
                              label: selectedClass[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL] as string,
                              value: selectedClass[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL] as string,
                            }
                          : { label: '(Use global default)', value: '' }
                      }
                      onChange={({ detail }) =>
                        onUpdateClass({ [X_AWS_IDP_EXTRACTION_ESCALATION_MODEL]: detail.selectedOption.value || undefined })
                      }
                      options={EXTRACTION_MODEL_OVERRIDE_OPTIONS}
                      filteringType="auto"
                      placeholder="Select escalation model override"
                    />
                  </FormField>

                  <FormField
                    label="Extraction System Prompt Override (Optional)"
                    description="Override the global extraction system prompt for this class. When set, this prompt is used instead of the global extraction.system_prompt setting. Leave blank to use the default."
                  >
                    <Textarea
                      value={(selectedClass[X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT] as string) || ''}
                      onChange={({ detail }) => onUpdateClass({ [X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT]: detail.value || undefined })}
                      placeholder="Use global default"
                      rows={4}
                    />
                  </FormField>

                  <FormField
                    label="Extraction Task Prompt Override (Optional)"
                    description="Override the global extraction task prompt for this class. Leave blank to use the default. Supports the same placeholders as the global task prompt: {DOCUMENT_CLASS}, {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}, {FEW_SHOT_EXAMPLES}, {DOCUMENT_TEXT}, {DOCUMENT_IMAGE}, and <<CACHEPOINT>>."
                  >
                    <Textarea
                      value={(selectedClass[X_AWS_IDP_EXTRACTION_TASK_PROMPT] as string) || ''}
                      onChange={({ detail }) => onUpdateClass({ [X_AWS_IDP_EXTRACTION_TASK_PROMPT]: detail.value || undefined })}
                      placeholder="Use global default"
                      rows={8}
                    />
                  </FormField>

                  <Header {...({ variant: 'h5' } as Record<string, unknown>)}>Evaluation Configuration</Header>

                  <FormField
                    label="Overall Match Threshold"
                    description={`Minimum weighted score for ${typeLabel}-level baseline evaluation match (0-1)`}
                  >
                    <Input
                      type="number"
                      {...({ step: '0.01', min: '0', max: '1' } as Record<string, unknown>)}
                      value={(selectedClass[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] as number)?.toString() || '0.8'}
                      onChange={({ detail }) => {
                        const value = detail.value ? parseFloat(detail.value) : 0.8;
                        if (value >= 0 && value <= 1) {
                          onUpdateClass({
                            [X_AWS_IDP_EVALUATION_MATCH_THRESHOLD]: value,
                          });
                        }
                      }}
                      placeholder="0.8"
                    />
                  </FormField>
                </>
              )}
            </>
          )}

          {usedIn.length > 0 && (
            <FormField
              label="Used In"
              description={`This class is referenced by ${usedIn.length} attribute${usedIn.length > 1 ? 's' : ''}`}
            >
              <SpaceBetween size="xs">
                {usedIn.map((usage) => (
                  <Button
                    key={`${usage.classId}-${usage.attributeName}`}
                    variant="inline-link"
                    iconName="external"
                    onClick={() => {
                      if (onNavigateToAttribute) {
                        onNavigateToAttribute(usage.classId, usage.attributeName);
                      } else if (onNavigateToClass) {
                        onNavigateToClass(usage.classId);
                      }
                    }}
                  >
                    {usage.className}.{usage.attributeName} ({usage.type === 'array' ? `${selectedClass.name}[]` : selectedClass.name})
                  </Button>
                ))}
              </SpaceBetween>
            </FormField>
          )}
        </SpaceBetween>
      </Box>
    );
  }

  if (!selectedAttribute || !selectedAttributeName) {
    return (
      <Box textAlign="center" padding="xxl">
        <Header variant="h3">No Selection</Header>
        <p>Select a class or attribute from the canvas to edit its properties</p>
      </Box>
    );
  }

  const [attributeLabel, setAttributeLabel] = useState(selectedAttributeName || '');

  useEffect(() => {
    setAttributeLabel(selectedAttributeName || '');
  }, [selectedAttributeName]);

  // When loading an existing schema with an invalid validation engine value,
  // overwrite it with "llm" (Requirement 8.6)
  useEffect(() => {
    if (isRuleSchema && selectedAttribute) {
      const currentValue = selectedAttribute[X_AWS_IDP_VALIDATION_ENGINE] as string | undefined;
      if (currentValue !== undefined && currentValue !== null) {
        const isValid = VALIDATION_ENGINE_OPTIONS.some((opt) => opt.value === currentValue);
        if (!isValid) {
          onUpdate({ [X_AWS_IDP_VALIDATION_ENGINE]: 'llm' });
        }
      }
    }
  }, [isRuleSchema, selectedAttribute, onUpdate]);

  const handleRenameSubmit = (): void => {
    const trimmed = attributeLabel.trim();
    if (!trimmed || trimmed === selectedAttributeName) {
      setAttributeLabel(selectedAttributeName || '');
      return;
    }

    if (onRenameAttribute && !onRenameAttribute(trimmed)) {
      setAttributeLabel(selectedAttributeName || '');
    }
  };

  return (
    <Box>
      <Header variant="h3">{isRuleSchema ? 'Rule Properties' : `Property Inspector: ${selectedAttributeName}`}</Header>
      <SpaceBetween size="m">
        <FormField label={isRuleSchema ? 'Rule Name' : 'Attribute Name'}>
          <Input
            value={attributeLabel}
            onChange={({ detail }) => setAttributeLabel(detail.value)}
            onBlur={handleRenameSubmit}
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            onKeyDown={(event: any) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                handleRenameSubmit();
              }
            }}
          />
        </FormField>

        <Checkbox checked={isRequired} onChange={({ detail }) => onToggleRequired(detail.checked)}>
          Required field
        </Checkbox>

        <FormField
          label={isRuleSchema ? 'Rule Output Data Type' : 'Type'}
          description={isRuleSchema ? 'The type of data this rule outputs' : 'JSON Schema type for this attribute'}
        >
          <Select
            selectedOption={
              TYPE_OPTIONS.find((opt) => opt.value === selectedAttribute.type) ||
              // If no type but has $ref, assume it's an object reference
              (selectedAttribute.$ref ? TYPE_OPTIONS.find((opt) => opt.value === 'object') : null) ||
              null
            }
            onChange={({ detail }) => {
              // When changing type, remove $ref if it exists (it's incompatible with inline type)
              const updates: Record<string, unknown> = { type: detail.selectedOption.value };
              if (selectedAttribute.$ref) {
                updates.$ref = undefined;
              }
              onUpdate(updates);
            }}
            options={TYPE_OPTIONS}
          />
        </FormField>

        {(selectedAttribute.type === 'object' || selectedAttribute.$ref) &&
          availableClasses &&
          availableClasses.length > 0 &&
          !isRuleSchema && (
            <>
              <FormField
                label="Reference Existing Class (Optional)"
                description="Link to a reusable class definition instead of defining properties inline"
              >
                <SpaceBetween size="xs">
                  <Select
                    selectedOption={
                      selectedAttribute.$ref
                        ? {
                            label: selectedAttribute.$ref.replace('#/$defs/', ''),
                            value: selectedAttribute.$ref,
                          }
                        : null
                    }
                    onChange={({ detail }) => {
                      if (detail.selectedOption.value) {
                        const updates: Record<string, unknown> = { ...selectedAttribute, $ref: detail.selectedOption.value };
                        // Remove inline object properties as they conflict with $ref
                        delete updates.properties;
                        delete updates.required;
                        delete updates.minProperties;
                        delete updates.maxProperties;
                        delete updates.additionalProperties;
                        // Note: Keep type as 'object' for UI purposes, but it won't be exported in the final schema
                        if (!updates.type) {
                          updates.type = 'object';
                        }
                        onUpdate(updates);
                      } else {
                        const updates: Record<string, unknown> = { ...selectedAttribute, $ref: undefined };
                        // Restore type to object when removing $ref
                        if (!updates.type) {
                          updates.type = 'object';
                        }
                        onUpdate(updates);
                      }
                    }}
                    options={[
                      { label: 'None (inline properties)', value: '' },
                      ...availableClasses.map((cls) => ({
                        label: cls.name,
                        value: `#/$defs/${cls.name}`,
                      })),
                    ]}
                    placeholder="Select a class to reference"
                  />
                  {selectedAttribute.$ref && (onNavigateToClass || onNavigateToAttribute) && (
                    <Button
                      iconName="external"
                      onClick={() => {
                        const className = selectedAttribute.$ref!.replace('#/$defs/', '');
                        const referencedClass = availableClasses.find((cls) => cls.name === className);
                        if (referencedClass) {
                          if (onNavigateToAttribute) {
                            onNavigateToAttribute(referencedClass.id, null);
                          } else if (onNavigateToClass) {
                            onNavigateToClass(referencedClass.id);
                          }
                        }
                      }}
                    >
                      Go to {selectedAttribute.$ref.replace('#/$defs/', '')} class
                    </Button>
                  )}
                </SpaceBetween>
              </FormField>

              {!selectedAttribute.$ref && <ObjectConstraints attribute={selectedAttribute} onUpdate={onUpdate} />}
            </>
          )}

        {selectedAttribute.type === 'array' && availableClasses && availableClasses.length > 0 && (
          <>
            <FormField label="Array Item Type" description="Define what each item in the array should be">
              <SpaceBetween size="xs">
                <Select
                  selectedOption={
                    selectedAttribute.items?.$ref
                      ? {
                          label: selectedAttribute.items.$ref.replace('#/$defs/', ''),
                          value: selectedAttribute.items.$ref,
                        }
                      : {
                          label: `Simple (${selectedAttribute.items?.type || 'string'})`,
                          value: 'simple',
                        }
                  }
                  onChange={({ detail }) => {
                    if (detail.selectedOption.value === 'simple') {
                      onUpdate({ items: { type: 'string' } });
                    } else {
                      onUpdate({ items: { $ref: detail.selectedOption.value } });
                    }
                  }}
                  options={[
                    { label: 'Simple (string)', value: 'simple' },
                    ...availableClasses.map((cls) => ({
                      label: `Class: ${cls.name}`,
                      value: `#/$defs/${cls.name}`,
                    })),
                  ]}
                />
                {selectedAttribute.items?.$ref && (onNavigateToClass || onNavigateToAttribute) && (
                  <Button
                    iconName="external"
                    onClick={() => {
                      const className = selectedAttribute.items!.$ref!.replace('#/$defs/', '');
                      const referencedClass = availableClasses.find((cls) => cls.name === className);
                      if (referencedClass) {
                        if (onNavigateToAttribute) {
                          onNavigateToAttribute(referencedClass.id, null);
                        } else if (onNavigateToClass) {
                          onNavigateToClass(referencedClass.id);
                        }
                      }
                    }}
                  >
                    Go to {selectedAttribute.items.$ref.replace('#/$defs/', '')} class
                  </Button>
                )}
              </SpaceBetween>
            </FormField>

            <ArrayConstraints attribute={selectedAttribute} onUpdate={onUpdate} availableClasses={availableClasses} />
          </>
        )}

        {isRuleSchema && (
          <FormField label="Description" description="Describe what information this rule validates and provide specific instructions.">
            <Textarea
              value={selectedAttribute.description || ''}
              onChange={({ detail }) => onUpdate({ description: detail.value || undefined })}
              rows={3}
              placeholder="e.g., Validates that the patient consent form is properly signed"
            />
          </FormField>
        )}

        {isRuleSchema && (
          <FormField label="Validation Engine" description="Choose the engine for validating this rule">
            <Select
              selectedOption={(() => {
                const currentValue = selectedAttribute[X_AWS_IDP_VALIDATION_ENGINE] as string | undefined;
                // If value exists and is valid, use it
                const validOption = VALIDATION_ENGINE_OPTIONS.find((opt) => opt.value === currentValue);
                if (validOption) {
                  return validOption;
                }
                // Default display: "Semantic (LLM)" when field is absent or invalid
                return VALIDATION_ENGINE_OPTIONS[0];
              })()}
              onChange={({ detail }) => {
                onUpdate({ [X_AWS_IDP_VALIDATION_ENGINE]: detail.selectedOption.value });
              }}
              options={VALIDATION_ENGINE_OPTIONS}
            />
          </FormField>
        )}

        {isRuleSchema && (
          <FormField label="Rule ID" description="Unique identifier for this rule (e.g., coverage_income_ratio)">
            <Input
              value={(selectedAttribute[X_AWS_IDP_RULE_ID] as string) || ''}
              onChange={({ detail }) => {
                onUpdate({ [X_AWS_IDP_RULE_ID]: detail.value || undefined });
              }}
              placeholder="e.g., coverage_income_ratio"
            />
          </FormField>
        )}

        {isRuleSchema && selectedAttribute[X_AWS_IDP_VALIDATION_ENGINE] === 'z3' && (
          <RuleJsonSection key={selectedAttributeName} selectedAttribute={selectedAttribute} onUpdate={onUpdate} />
        )}

        {!isRuleSchema && <MetadataFields attribute={selectedAttribute} onUpdate={onUpdate} />}

        {!isRuleSchema && <StringConstraints attribute={selectedAttribute} onUpdate={onUpdate} />}

        {!isRuleSchema && <NumberConstraints attribute={selectedAttribute} onUpdate={onUpdate} />}

        {!isRuleSchema && <ValueConstraints attribute={selectedAttribute} onUpdate={onUpdate} />}

        {!isRuleSchema &&
          (() => {
            const declaredPageTypes = (selectedClass?.[X_AWS_IDP_PAGE_TYPES] as PageTypeEntry[] | undefined) || [];
            if (declaredPageTypes.length === 0) {
              return null;
            }
            const selectedSourcePages = (selectedAttribute[X_AWS_IDP_SOURCE_PAGE_TYPES] as string[] | undefined) || [];
            const options = declaredPageTypes
              .filter((pt) => pt.name)
              .map((pt) => ({
                label: pt.name,
                value: pt.name,
                description: pt.description || undefined,
              }));
            return (
              <>
                <Header {...({ variant: 'h4' } as Record<string, unknown>)}>Missing-Page Handling</Header>
                <FormField
                  label="Source Page Types (Optional)"
                  description="Page sub-types this property is sourced from. If none of the selected page types are present in a section, the property is treated as MISSING per extraction.missing_field_handling config (instead of being silently empty/BLANK)."
                >
                  <Multiselect
                    selectedOptions={options.filter((opt) => selectedSourcePages.includes(opt.value))}
                    onChange={({ detail }) => {
                      const values = detail.selectedOptions.map((opt) => opt.value).filter((v): v is string => Boolean(v));
                      onUpdate({
                        [X_AWS_IDP_SOURCE_PAGE_TYPES]: values.length > 0 ? values : undefined,
                      });
                    }}
                    options={options}
                    placeholder="Select page types where this field appears"
                    empty="No page types declared on the parent class"
                  />
                </FormField>
              </>
            );
          })()}

        {!isRuleSchema && (
          <>
            <Header {...({ variant: 'h4' } as Record<string, unknown>)}>Assessment Configuration</Header>

            <FormField
              label="Confidence Threshold"
              description="Minimum confidence score for extraction quality - triggers alert if below this threshold (0-1)"
            >
              <Input
                type="number"
                {...({ step: '0.01', min: '0', max: '1' } as Record<string, unknown>)}
                value={(selectedAttribute[X_AWS_IDP_CONFIDENCE_THRESHOLD] as number)?.toString() || ''}
                onChange={({ detail }) =>
                  onUpdate({
                    [X_AWS_IDP_CONFIDENCE_THRESHOLD]: detail.value ? parseFloat(detail.value) : undefined,
                  })
                }
                placeholder="e.g., 0.9"
              />
            </FormField>

            <Header {...({ variant: 'h4' } as Record<string, unknown>)}>Evaluation Configuration (Baseline Accuracy)</Header>
          </>
        )}

        {!isRuleSchema &&
          (() => {
            // Detect if this is a structured array (List[Object])
            // Must check BOTH inline objects AND $ref to classes (matches backend logic)
            const isStructuredArray =
              selectedAttribute.type === 'array' && (selectedAttribute.items?.type === 'object' || selectedAttribute.items?.$ref);

            // Filter available methods based on field type
            const availableMethods = EVALUATION_METHOD_OPTIONS.filter((opt) => {
              // HUNGARIAN requires structured array
              if (opt.requiresStructuredItems) {
                return isStructuredArray;
              }
              // Methods with validFor restrictions
              if (opt.validFor) {
                // For arrays with SIMPLE items (Array[String], Array[Number], etc.)
                // check if method is valid for the ITEM type
                if (selectedAttribute.type === 'array' && !isStructuredArray) {
                  const itemType = selectedAttribute.items?.type || 'string';
                  return opt.validFor.includes(itemType);
                }
                // For structured arrays (Array[Object]), check if method is valid for arrays
                if (selectedAttribute.type === 'array' && isStructuredArray) {
                  return opt.validFor.includes('array');
                }
                // For other types, check directly
                return opt.validFor.includes(selectedAttribute.type as string);
              }
              // Default: allow for non-structured-arrays
              return !isStructuredArray;
            });

            const currentMethod = selectedAttribute[X_AWS_IDP_EVALUATION_METHOD] as string | undefined;

            return (
              <>
                <FormField label="Evaluation Method" description="Comparison algorithm for baseline accuracy assessment">
                  <Select
                    selectedOption={availableMethods.find((opt) => opt.value === currentMethod) || null}
                    onChange={({ detail }) => {
                      const method = detail.selectedOption.value ?? '';
                      const updates: Record<string, unknown> = {
                        [X_AWS_IDP_EVALUATION_METHOD]: method,
                      };

                      // Auto-set appropriate threshold based on field type
                      if (isStructuredArray) {
                        // For structured arrays, use match_threshold
                        if ((EVALUATION_MATCH_THRESHOLD_DEFAULTS as Record<string, number>)[method]) {
                          updates[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] = (EVALUATION_MATCH_THRESHOLD_DEFAULTS as Record<string, number>)[
                            method
                          ];
                        }
                        // Clear regular threshold if present
                        updates[X_AWS_IDP_EVALUATION_THRESHOLD] = undefined;
                      } else {
                        // For regular fields, use threshold
                        if ((EVALUATION_THRESHOLD_DEFAULTS as Record<string, number>)[method]) {
                          updates[X_AWS_IDP_EVALUATION_THRESHOLD] = (EVALUATION_THRESHOLD_DEFAULTS as Record<string, number>)[method];
                        }
                        // Clear match_threshold if present
                        updates[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] = undefined;
                      }

                      onUpdate(updates);
                    }}
                    options={availableMethods}
                    placeholder="Select evaluation method"
                  />
                </FormField>

                {/* Show match-threshold for structured arrays */}
                {isStructuredArray && currentMethod && METHODS_REQUIRING_MATCH_THRESHOLD.includes(currentMethod) && (
                  <FormField
                    label="Match Threshold"
                    description="Minimum score for matching items in the array (0-1). Stickler uses Hungarian algorithm to find optimal item pairing."
                  >
                    <Input
                      type="number"
                      {...({ step: '0.01', min: '0', max: '1' } as Record<string, unknown>)}
                      value={(selectedAttribute[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] as number)?.toString() || ''}
                      onChange={({ detail }) =>
                        onUpdate({
                          [X_AWS_IDP_EVALUATION_MATCH_THRESHOLD]: detail.value ? parseFloat(detail.value) : undefined,
                        })
                      }
                      placeholder={`Default: ${(EVALUATION_MATCH_THRESHOLD_DEFAULTS as Record<string, number>)[currentMethod] || '0.8'}`}
                    />
                  </FormField>
                )}

                {/* Show threshold for non-array fields */}
                {!isStructuredArray && currentMethod && METHODS_REQUIRING_THRESHOLD.includes(currentMethod) && (
                  <FormField label="Evaluation Threshold" description="Minimum similarity score to consider a baseline match (0-1)">
                    <Input
                      type="number"
                      {...({ step: '0.01', min: '0', max: '1' } as Record<string, unknown>)}
                      value={(selectedAttribute[X_AWS_IDP_EVALUATION_THRESHOLD] as number)?.toString() || ''}
                      onChange={({ detail }) =>
                        onUpdate({
                          [X_AWS_IDP_EVALUATION_THRESHOLD]: detail.value ? parseFloat(detail.value) : undefined,
                        })
                      }
                      placeholder={`Default: ${(EVALUATION_THRESHOLD_DEFAULTS as Record<string, number>)[currentMethod] || ''}`}
                    />
                  </FormField>
                )}

                {/* Show weight for non-array fields */}
                {!isStructuredArray && (
                  <FormField
                    label="Evaluation Weight"
                    description="Field importance for business criticality (1.0=normal, 2.0=critical, 0.5=optional)"
                  >
                    <Input
                      type="number"
                      {...({ step: '0.1', min: '0.1' } as Record<string, unknown>)}
                      value={(selectedAttribute[X_AWS_IDP_EVALUATION_WEIGHT] as number)?.toString() || '1.0'}
                      onChange={({ detail }) => {
                        const value = detail.value ? parseFloat(detail.value) : 1.0;
                        // Validate minimum
                        if (value >= 0.1) {
                          onUpdate({
                            [X_AWS_IDP_EVALUATION_WEIGHT]: value,
                          });
                        }
                      }}
                      placeholder="1.0"
                    />
                  </FormField>
                )}

                {/* Info alert for structured arrays */}
                {isStructuredArray && currentMethod === EVALUATION_METHOD_HUNGARIAN && (
                  <Alert type="info">
                    <strong>Hungarian Matching</strong>
                    <br />
                    Stickler uses the Hungarian algorithm to find the optimal pairing between expected and actual list items. The match
                    threshold you set applies to individual item comparisons.
                  </Alert>
                )}
              </>
            );
          })()}
      </SpaceBetween>
    </Box>
  );
};

// Memoize the component to prevent re-renders when props haven't changed
export default React.memo(SchemaInspector);
